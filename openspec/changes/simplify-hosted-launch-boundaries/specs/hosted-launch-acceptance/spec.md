## Purpose

Define reproducible hosted launch evidence that proves useful service behavior, continuity and recoverability without requiring sustained operator attendance.

## ADDED Requirements

### Requirement: Runtime and distribution acceptance are separate claims

The launch workflow SHALL record runtime activation, customer service acceptance and each client artifact certification separately. Runtime activation MUST retain signed identity, strict compatibility and lifecycle fences without requiring client artifact certification. Client certification MUST require genuine evidence from the host and exact artifact being claimed.

#### Scenario: Runtime and custom-client service pass before marketplace review

- **WHEN** runtime safety and supported custom-client service acceptance pass while marketplace artifacts remain uncertified
- **THEN** the invite-only service can be accepted without claiming marketplace approval or certifying an untested host

### Requirement: Acceptance is resumable and normally agent operated

The acceptance workflow SHALL execute independent checks without continuous operator attendance and persist a content-safe run report with immutable release/contract identity, stage outcomes, rerunnable commands and exact blocked actions. It MUST distinguish passed, failed, pending and blocked stages. It MUST use the ordinary customer security boundary, not a production authorization bypass.

#### Scenario: Session ends during a long continuity check

- **WHEN** the orchestrating agent reconnects to an existing acceptance run
- **THEN** it can determine completed and outstanding stages without resetting the tenant or repeating a committed mutation

#### Scenario: Host requires human consent

- **WHEN** an external platform requires an action the available authorized automation cannot perform
- **THEN** the runner records one precise operator action and checkpoints the blocked stage
- **AND** independent automated checks continue without labeling the host certified

### Requirement: Useful memory acceptance includes semantic retrieval and continuity

Service acceptance SHALL demonstrate public OAuth, initialization, canonical tool discovery, durable capture and paraphrased semantic recall with a resolvable citation on a representative corpus. It MUST demonstrate fresh-client access, rotating refresh beyond the configured access-token lifetime and successful calls after a fleet credential renewal window. Process health or index counts alone MUST NOT satisfy these claims.

#### Scenario: A captured fact is recalled in a fresh client

- **WHEN** a test client captures a run-specific fact, required indexing converges and a fresh authorized client asks a paraphrased question
- **THEN** recall returns the expected fact with a resolvable citation belonging to that tenant

#### Scenario: Access and fleet credentials renew

- **WHEN** the acceptance run crosses an access-token expiry and a fleet credential renewal window
- **THEN** normal rotation and subsequent capture/recall succeed without reprovisioning the tenant

### Requirement: Acceptance proves denial and recoverability

The acceptance suite SHALL exercise revoked families, refresh replay, wrong audience/client, suspension, cross-tenant sentinel isolation, concurrent lifecycle changes, database/cell outages and lost-response mutation retry. It MUST demonstrate backup recoverability through an isolated restore without overwriting the source tenant.

#### Scenario: Concurrent tenants use identical keys and paths

- **WHEN** two isolated fixture tenants invoke operations with identical paths and idempotency keys but distinct sentinel content
- **THEN** results, errors, replay records and logs never cross tenant boundaries

#### Scenario: Backup is restored for verification

- **WHEN** the governed restore drill runs against a reserved isolated target
- **THEN** the expected fixture content and integrity checks pass while the original tenant remains unchanged

### Requirement: Performance claims include authenticated measured evidence

Alpha performance acceptance SHALL report cold/warm latency separately from a declared vantage point and fixed corpus, with at least 100 warm samples per operation and 20 declared cold runs. At five concurrent clients across two reserved synthetic tenants, the initial targets SHALL be warm p95 initialization/tool listing at most 500 ms, small durable capture and citation-bearing recall at most one second, and cold authenticated initialization at most two seconds. Model thinking and human consent time MUST be excluded explicitly, not hidden. A missed target MUST remain a failed performance stage with measured attribution. This workload MUST NOT be represented as a five-cell capacity test.

#### Scenario: Gateway placement reduces network work

- **WHEN** old and new paths are measured against the same realistic corpus and workload
- **THEN** the report contains p50/p95, error counts, gateway/database/cell stage timing and exact release/configuration identity
- **AND** it does not substitute unauthenticated health or 401 latency for authenticated performance

### Requirement: Test resources are owned and non-destructive

Repeated acceptance SHALL reuse designated synthetic tenants and isolate fixtures by run. Default automated tests MUST use disposable local state without implicitly creating paid preview deployments or database branches. Cloud resources MUST have explicit ownership and lifecycle records. Cleanup MUST target only owned run fixtures and MUST preserve user data and required evidence.

#### Scenario: Acceptance reruns after an agent interruption

- **WHEN** a prior run left resources or completed mutations
- **THEN** the next run resumes or reconciles its owned manifest without deleting an owner vault or allocating unbounded replacement tenants or branches
