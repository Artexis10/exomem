## Purpose

Let a hosted cell complete governed writes promptly while the control plane retains durable authority over activation and interrupted writes recover without duplicate effects.

## ADDED Requirements

### Requirement: Cell-scoped acknowledgement authority

The acknowledgement service SHALL authenticate a currently accepted credential for the owned cell, derive its tenant, attachment and callback identity from trusted state, and verify a fresh challenge-bound proof of the exact committed successor through the private runtime control path. Caller-supplied tuple values SHALL NOT constitute proof. The service SHALL permit only the next activation epoch of the same store and attachment or the identical already-acknowledged successor. It SHALL preserve enrollment, keyring, membership, lifetime and attachment authority and revalidate freshness immediately before publication.

#### Scenario: Ordinary committed successor
- **WHEN** an authenticated cell presents an exact committed next publication under current authority
- **THEN** the control plane durably publishes its signed successor using the current external revision
- **AND** unchanged authority fields remain unchanged

#### Scenario: Invalid or stale request
- **WHEN** the request uses a wrong cell credential, changed attachment, expired authority, replayed challenge, skipped epoch, forged proof or competing successor
- **THEN** acknowledgement is refused without changing external authority

#### Scenario: Renewal wins the revision race
- **WHEN** renewal changes the external revision before acknowledgement publication
- **THEN** the acknowledgement rereads and verifies the latest authority and rebuilds only the permitted activation change
- **AND** it never replaces renewal fields with stale values

### Requirement: Prompt protected delivery

The custody publisher SHALL obtain acknowledged signed control and membership records over a fixed authenticated internal TLS destination and verify them using its existing projected keyring. This endpoint SHALL return no keyring or signing-key bytes and grant no provisioning, enrollment, renewal or cross-cell authority. Only the custody publisher SHALL write the runtime projection. The healthy write SHALL complete within the hosted command deadline without waiting for Kubernetes Secret propagation.

#### Scenario: Healthy capture
- **WHEN** committed publication, authoritative CAS and immediate custody delivery succeed
- **THEN** the runtime verifies external/store parity and returns the ordinary successful capture result within the command deadline
- **AND** its custody mount remains read-only

#### Scenario: Rotated verification key is not available
- **WHEN** delivered records require a key absent from the current projected keyring
- **THEN** delivery remains pending until authenticated key propagation
- **AND** no service credential can retrieve signing keys as a shortcut

#### Scenario: Transport authority is unavailable
- **WHEN** the TLS identity, trust bundle, endpoint capability or required credential is missing or invalid before mutation
- **THEN** mutation is refused before canonical effects
- **AND** there is no insecure transport fallback or caller-selected destination

### Requirement: Bounded acknowledgement recovery

An interrupted acknowledgement SHALL retain the exact publication and mutation identity. The system SHALL distinguish prepared/uncommitted, committed/pending and acknowledged outcomes. Content serving SHALL remain blocked during external/store mismatch. A control-plane CAS whose response was lost SHALL be recognized by exact successor identity; recovery SHALL NOT replay canonical effects or renew expired authority.

#### Scenario: Response is lost after external commit
- **WHEN** the authoritative successor commits but its response or local delivery is lost
- **THEN** a same-identity retry retrieves and verifies that successor and recovers the original mutation outcome
- **AND** no second note, publication or activation advance is created

#### Scenario: Callback while capture holds its fences
- **WHEN** a capture waits for acknowledgement while holding its normal mutation fences
- **THEN** the bounded private proof callback can verify the already committed publication without waiting on those fences
- **AND** it cannot expose content or authorize a different publication

#### Scenario: Ordinary command workers are saturated
- **WHEN** ordinary command workers are occupied by calls waiting on mutation fences
- **THEN** reserved bounded proof execution capacity still allows the active publication's callback to complete
- **AND** excess proof requests cannot create an unbounded queue or exhaust ordinary serving capacity
