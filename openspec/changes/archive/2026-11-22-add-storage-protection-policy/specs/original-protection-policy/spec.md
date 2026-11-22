## Purpose

Let an owner set the verified replica coverage needed to protect original files, and add missing protection within an explicit transfer allowance.

## ADDED Requirements

### Requirement: Set minimum verified protection and a transfer allowance

An owner MAY opt into a storage protection policy with minimum recoverable copy and distinct failure-domain counts, eligible declared stores, and a monthly byte allowance for policy copies. Each store's verified copies SHALL use exact version and binding evidence, verified content, current key custody, and known absence of quarantine. A distinct domain SHALL count once regardless of store or filing count. Unavailable or incomplete evidence SHALL remain unknown. The system SHALL retain the existing behavior when no policy is present.

#### Scenario: Two stores share a failure domain

- **WHEN** two exact, verified versions use the same configured failure domain
- **THEN** they count as two copies in one domain
- **AND** a presence check on an additional version does not meet a verified copy requirement

#### Scenario: A store or exact proof is unavailable

- **WHEN** the current policy cannot establish that an exact copy is recoverable
- **THEN** the owner can see the missing proof or unavailable store
- **AND** the worker treats the gap as unfinished work

### Requirement: Add and verify bounded replica work

The media worker SHALL reconcile an opted-in policy through the existing replica and verification paths. It SHALL add eligible copies and request missing content proof without removing an existing copy. It SHALL reserve and charge automatic transfer allowance durably across concurrent attempts and retries. The operation SHALL leave owner-requested replica work and repair demand independent.

#### Scenario: The next copy exceeds the allowance

- **WHEN** a candidate needs more unreserved bytes than the current policy allows this month
- **THEN** the worker leaves the copy pending with a budget reason
- **AND** it sends no ciphertext on that attempt

#### Scenario: Work resumes after interruption

- **WHEN** the worker stops during policy convergence
- **THEN** a fresh worker resumes bounded eligible work
- **AND** it neither charges a settled version again nor cancels a separate owner request

### Requirement: Fence owner-authorized changes that reduce protection

Managed placement SHALL require owner authority for a protection downgrade or an eviction under an opted-in policy. Immediately before unlink, the eviction writer SHALL verify that each affected binding still has the required recoverable copies and domains after removal. A managed placement operation SHALL preserve at least one verified recoverable copy of each still-live original. Intentional governed deletion retains its existing authority and Trash contract.

#### Scenario: A shared original has a filing without enough protection

- **WHEN** two filings share one local object and only one retains the required verified copies
- **THEN** eviction retains the physical original and reports the unmet policy

#### Scenario: An otherwise qualifying replica enters quarantine

- **WHEN** a policy copy is quarantined after candidate inspection and before the local unlink
- **THEN** the final custody check excludes that copy
- **AND** eviction retains the original if the remaining copies do not meet policy

#### Scenario: External damage removes the last healthy copy

- **WHEN** damage outside the application makes all verified copies unrecoverable
- **THEN** the next available evidence reports the loss or uncertainty
- **AND** managed placement removes no additional copy
