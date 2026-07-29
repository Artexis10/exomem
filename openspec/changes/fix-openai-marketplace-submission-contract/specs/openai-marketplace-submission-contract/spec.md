## ADDED Requirements

### Requirement: Current OpenAI listing limits
The system SHALL reject an OpenAI Plugin Directory candidate whose public fields exceed the current provider limits or contain release-stage language that represents the production plugin as a private alpha, trial, demo, hypothetical product, or not-yet-built service.

#### Scenario: Short description exceeds the portal limit
- **WHEN** the OpenAI short description is longer than 30 characters
- **THEN** marketplace validation fails before rendering or submission readiness

#### Scenario: Starter prompt exceeds the portal limit
- **WHEN** any OpenAI starter prompt is longer than 128 characters
- **THEN** marketplace validation fails with the affected field identified

#### Scenario: Public listing uses trial-stage language
- **WHEN** an OpenAI title, description, short description, release note, or starter prompt describes the production plugin as private alpha, trial, demo, hypothetical, or not yet built
- **THEN** marketplace validation fails
- **AND** internal launch policy and operator-only documentation remain free to describe the actual cohort state honestly

### Requirement: Complete OpenAI review material
The system SHALL render a deterministic OpenAI review packet that includes a recording handoff, complete tool contracts, and a non-empty justification for every boolean MCP annotation.

#### Scenario: Tool annotation lacks justification
- **WHEN** an OpenAI tool has a read-only, destructive, idempotent, or open-world annotation without a matching explanation
- **THEN** packet validation fails before submission readiness

#### Scenario: Review recording is not prepared
- **WHEN** signed OpenAI prerequisite evidence does not attest that the required walkthrough recording is prepared
- **THEN** the OpenAI candidate is not submission-ready
- **AND** no raw recording URL is required in tracked inputs or generated artifacts

#### Scenario: Review material is complete
- **WHEN** the packet contains current listing fields, complete tool schemas and annotations, deterministic annotation explanations, review cases, and an operator-supplied recording handoff
- **THEN** static OpenAI review-material validation passes

### Requirement: Versioned deterministic reviewer fixture
The system SHALL bind positive marketplace review cases to a versioned, generic, non-sensitive canonical fixture payload with a verified digest and reset semantics, and SHALL keep live reviewer identities, tenant exports, and content-bearing production evidence outside Git.

#### Scenario: Positive case references seeded content
- **WHEN** a positive review case exercises retrieval or capture against the reviewer tenant
- **THEN** it names one or more known fixture references from the current manifest
- **AND** its expected outcome is deterministic without embedding a tenant ID, credential, invitation, or production token

#### Scenario: Write-capable case is retried
- **WHEN** a positive review case creates or changes fixture content
- **THEN** the fixture defines an exact disposable key and idempotent reset procedure
- **AND** repeated provider review starts from the same canonical payload digest

#### Scenario: Fixture reference is stale or unknown
- **WHEN** review cases declare a different fixture version or an unknown fixture reference
- **THEN** marketplace validation fails before packet rendering

#### Scenario: Fixture payload digest drifts
- **WHEN** the canonical generic content no longer hashes to the declared payload digest
- **THEN** marketplace validation fails before packet rendering or submission readiness

#### Scenario: Live reviewer tenant is prepared
- **WHEN** an operator seeds the dedicated reviewer tenant
- **THEN** the tenant content matches the checked-in fixture version
- **AND** native-client acceptance remains required to prove the live content-bearing flow

### Requirement: Live reviewer access evidence
The system SHALL require fresh signed, secret-free evidence that each provider can authenticate to the matching deployed reviewer flow and exercise the exact fixture bound by the packet.

#### Scenario: Reviewer access is live
- **WHEN** signed reviewer evidence binds the channel, matching provider, trusted deployment SHA, enabled feature state, active credential state, fixture version, payload digest, and a credential expiry beyond the minimum review window
- **THEN** the reviewer-access component of submission readiness passes

#### Scenario: Reviewer credential or fixture is stale
- **WHEN** the feature is disabled, the credential is absent/revoked/expired/too near expiry, the provider mismatches the channel, or the fixture version/digest mismatches the packet
- **THEN** the channel remains not submission-ready

#### Scenario: Reviewer evidence contains private material
- **WHEN** reviewer-access evidence contains a raw username, password, credential identifier, user ID, tenant ID, invitation, sample content, or bearer token
- **THEN** evidence validation fails with a redacted diagnostic

### Requirement: Staged marketplace readiness
The system SHALL distinguish provider-submission readiness from broad-public activation while preserving the stronger public-admission and non-reviewer evidence gates.

#### Scenario: Reviewer-ready but broad admission is closed
- **WHEN** static validation, live production probes, package promotion, provider registration, verified publisher, policy approval, provider-matched reviewer-access evidence, recording evidence where required, and channel-specific prerequisites pass but signed public-admission evidence is incomplete
- **THEN** the channel reports `submission_ready` as true
- **AND** existing broad-launch `ready` and `public` status remain false

#### Scenario: Candidate enters provider review
- **WHEN** a draft or submitted candidate is submission-ready and a valid provider receipt is supplied
- **THEN** transitions to submitted, in-review, or approved may be recorded
- **AND** no public activation pointer changes

#### Scenario: Provider publication is recorded
- **WHEN** an approved candidate is to be recorded as published
- **THEN** the stronger broad-launch gate, including signed public-admission evidence, MUST pass

#### Scenario: Public activation occurs
- **WHEN** a published revision also has fresh non-reviewer directory-install evidence for OAuth, discovery, governed recall, durable capture, later-chat recall, no-capture behavior, and revocation
- **THEN** the existing compare-and-swap activation may mark that exact revision public

### Requirement: Universal package identity remains stable
The system SHALL preserve one universal OpenAI package for ChatGPT and Codex and SHALL keep the registered package identity separate from any provider-issued directory identity.

#### Scenario: Corrected artifacts are regenerated
- **WHEN** the listing contract or review material changes
- **THEN** the OpenAI package, locks, archive, and directory packet regenerate deterministically against the existing `asdk_app_*` identity
- **AND** no `plugin_asdk_app_*` value enters the package manifest or lock files
