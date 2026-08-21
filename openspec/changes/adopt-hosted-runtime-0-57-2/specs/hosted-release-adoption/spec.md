## ADDED Requirements

### Requirement: Hosted release adoption binds one exact published runtime

The system SHALL adopt Exomem Hosted release `0.57.2` only from the exact stable release tag, source commit, immutable OCI digest, runtime-candidate bytes, and attestations produced by the allowed release workflow. The adopted target SHALL remain protocol `1` and profile `hosted-alpha-agent-v1`; mutable tags, an unverified image, a different source revision, or an unsigned candidate MUST be rejected.

#### Scenario: Exact stable runtime is selected

- **WHEN** the `0.57.2` tag, source commit, OCI subject, image attestation, and candidate-file attestation all verify against the reviewed release
- **THEN** the immutable image is eligible to become the forward runtime target
- **AND** its exact identities are recorded in the deployment lock

#### Scenario: Mutable or mismatched runtime is supplied

- **WHEN** any release, source, digest, workflow, attestation, architecture, profile, or protocol identity differs from the reviewed `0.57.2` target
- **THEN** release composition or deployment fails before the image can be assigned to a cell

### Requirement: Contract adoption is cross-repository and complete

Substrate SHALL trust the exact `0.57.2` v1 agent and gateway contracts before Exomem composes a deployment lock that names that consumer. Every production release-pinned mapping and operator runbook assertion SHALL resolve `0.57.2` to its exact fixture rather than a null, previous-release, generated-head, or mutable value. Release `0.54.1` SHALL remain representable as a legacy target.

#### Scenario: New release reaches every pinned site

- **WHEN** the `0.57.2` trusted-release companion is validated
- **THEN** contract storage, bootstrap/operator controls, lifecycle selection, canaries, admin catalog, gateway mapping, integration fixtures, and runbook checks all select the exact `0.57.2` identity
- **AND** PostgreSQL-backed and unit contract tests agree on the same digests

#### Scenario: One release mapping is omitted

- **WHEN** any required release-to-fixture or release-to-digest site returns null, `0.54.1`, or another identity for `0.57.2`
- **THEN** validation fails before the Substrate consumer commit can be used in deployment-lock composition

### Requirement: Mixed-version adoption preserves existing tenants

Release adoption SHALL select the default runtime only for future cells. The expand lock SHALL retain `0.54.1` in the authoritative legacy catalog, and a contract-mode cutover MUST be refused while any routable cell reports a legacy release. Applying either release catalog or expand lock MUST NOT enqueue an existing-cell lifecycle operation or mutate tenant storage, vault bytes, state, binding, credentials, entitlement, OAuth grants, or routing assignment.

#### Scenario: Legacy cell exists during adoption

- **WHEN** the `0.57.2` expand lock is deployed while a routable cell remains on `0.54.1`
- **THEN** the cell continues serving its assigned runtime and contract without a restart or data mutation
- **AND** the platform remains in expand mode

#### Scenario: Contract cutover is attempted with a legacy cell

- **WHEN** the routable-cell census contains any release other than the forward target
- **THEN** the contract-mode cutover is refused before changing admission
- **AND** every existing cell remains routable under expand

#### Scenario: No tenant cells exist

- **WHEN** the routable-cell census is empty and the `0.57.2` lock is adopted
- **THEN** no tenant resource is created, changed, restarted, or deleted by adoption itself
- **AND** the next separately authorized provision operation receives `0.57.2`

### Requirement: Reviewer bootstrap client reuse is explicit and bounded

The promotion harness SHALL accept an optional operator-supplied existing bootstrap client ID. Reuse SHALL go through the same server-side pinned registration as a new client and SHALL succeed only when the existing operator-managed record has the exact Claude loopback configuration and has never been authorized for a reviewer bootstrap. The harness MUST NOT auto-select a client, delete a client, widen a partition, enable a client directly, or continue after an ineligible reuse.

#### Scenario: Eligible client is reused at partition capacity

- **WHEN** the operator partition is full and `prepare` receives the ID of a disabled, exact-configuration, never-authorized pinned client
- **THEN** the server rebinds that record to the new staged release without inserting another client
- **AND** `prepare` records the explicit reuse and creates the invite only after registration succeeds

#### Scenario: Reused client differs or was authorized

- **WHEN** the supplied client has another platform, redirect/configuration digest, admission mode, or prior reviewer authorization
- **THEN** registration fails before invite or bootstrap-authority creation
- **AND** the harness does not fall back to a different stored client

#### Scenario: No reusable client is supplied

- **WHEN** `prepare` runs without an explicit existing client ID and operator capacity is available
- **THEN** it preserves the existing behavior of generating and registering one unique pinned loopback client

### Requirement: Promotion spends authority only after free preflight

The promotion flow SHALL verify candidate state, exact repository locks, absence of a live cohort and active bootstrap authority, runtime and provision capacity, stage availability, OpenAI connector identity, and bootstrap-client eligibility before creating the irreversible reviewer authority. It SHALL preserve the existing prepare/run boundary and MUST NOT claim success until both client evidence chains are imported within their actual assignment validity.

#### Scenario: Free preflight is green

- **WHEN** every release, digest, capacity, stage, connector, and client prerequisite verifies
- **THEN** `prepare` is permitted to attach locks, create staged artifacts, register the bootstrap client, and create the reviewer-purpose invite
- **AND** no authority or tenant is created until the operator starts `run`

#### Scenario: A free prerequisite is red

- **WHEN** any prerequisite differs from the reviewed candidate or available production state
- **THEN** the flow refuses before spending the invite or starting the authority clock
- **AND** it reports a bounded actionable failure without a secret

#### Scenario: Both evidence chains complete

- **WHEN** the reviewer cell reports the exact `0.57.2` runtime identity and clean Claude and OpenAI clients complete their staged evidence before assignment expiry
- **THEN** both evidence chains are eligible to be signed, imported, and used for cohort promotion

### Requirement: Personal-account acceptance proves the deployed alpha

The release SHALL be considered adopted only after a non-reviewer personal account authorizes through the promoted cohort and proves the expected runtime, read/write behavior, and reconnect semantics. Acceptance SHALL also prove that no unfinished lifecycle operation, active reviewer authority, stray capacity reservation, or unintended ordinary tenant was introduced.

#### Scenario: Full alpha acceptance succeeds

- **WHEN** the personal account connects through each supported client, receives runtime `0.57.2`, bootstraps, recalls, writes one governed item, reads it back, and reconnects with refresh authority
- **THEN** the Hosted alpha is eligible to be declared ready on `0.57.2`
- **AND** the personal tenant remains live after acceptance

#### Scenario: Acceptance fails after a tenant exists

- **WHEN** a non-reviewer tenant exists but runtime identity, behavior, or reconnect acceptance fails
- **THEN** the rollout stops without deleting, relabelling, downgrading, or mutating that tenant
- **AND** recovery requires a separately authorized operation

#### Scenario: Failed reviewer ceremony is cleaned up

- **WHEN** a reviewer-purpose ceremony fails and the documented cleanup is explicitly invoked for that reviewer tenant
- **THEN** only the identified reviewer-purpose resources are superseded and destroyed
- **AND** no ordinary tenant or persistent volume outside that scope is selected
