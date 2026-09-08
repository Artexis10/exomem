## Purpose

Define reproducible hosted launch evidence that proves useful service behavior, continuity and recoverability without requiring sustained operator attendance.

## ADDED Requirements

### Requirement: Runtime and distribution acceptance are separate claims

The launch workflow SHALL record runtime activation, customer service acceptance and each client artifact certification separately. Runtime activation MUST retain signed identity, strict compatibility and lifecycle fences without requiring client artifact certification. Client certification MUST require genuine evidence from the host and exact artifact being claimed.

#### Scenario: Runtime and custom-client service pass before marketplace review

- **WHEN** runtime safety and supported custom-client service acceptance pass while marketplace artifacts remain uncertified
- **THEN** the invite-only service can be accepted without claiming marketplace approval or certifying an untested host

### Requirement: Hosted governance activation is verified independently of runtime release

Service acceptance SHALL require the actual governance schema, authenticated provisioner-owned enrollment and activation tuple, serving membership and canonical authorization-session readiness to agree. Runtime-release activation, process health and a claimed schema constant MUST NOT substitute for this evidence. The hosted integration MUST reuse the canonical fenced migration and retain provisioner ownership of custody rather than invoking standalone custody initialization or enrollment.

#### Scenario: Healthy runtime still has unactivated governance

- **WHEN** authenticated core health passes but the actual store is schema v3 or authenticated control is unenrolled or lacks the matching activation tuple
- **THEN** the governance activation stage fails or remains explicitly pending
- **AND** the cell is not reported as ready for customer service acceptance

#### Scenario: Hosted migration completes under its existing custody

- **WHEN** the provisioner coordinates a verified backup, replica fencing, irreversible enrollment and the existing migration against the exact cell and vault
- **THEN** successful activation proves schema v4 and a matching authenticated enrollment, activation tuple and serving membership
- **AND** the runtime passes canonical authorization-session readiness without replacing hosted custody with standalone custody

#### Scenario: Hosted migration is interrupted or its acknowledgement is lost

- **WHEN** migration, enrollment publication or serving restoration is interrupted
- **THEN** retry reconciles the same durable operation and preserves fencing until its actual phase and custody state are verified
- **AND** stale membership or control cannot reopen admission or authorize a second cutover
- **AND** enrollment remains monotonic even when an explicit offline recovery restores the predecessor schema
- **AND** recovery does not restore v3 over acknowledged v4 writes

#### Scenario: Worker lease expires before migration Job completion

- **WHEN** a migration has committed its initial durable checkpoint and its worker loses authority while a Job is running or its submission acknowledgement is uncertain
- **THEN** a different operation cannot perform overlapping cell effects or tenant destruction, even after the worker lease expires or a newer fence is submitted
- **AND** claim selection leaves unrelated cells eligible and permits only the same internal operation to retry under current authority
- **AND** terminal failure preserves the migration denial barrier until explicit verified recovery resolves it
- **AND** successful completion clears the barrier only in the atomic commit that stores the final result and final operation state

#### Scenario: Target start or route publication outlasts serving custody

- **WHEN** the same migration retains its completion or confirmation checkpoint after a lost acknowledgement and its exact enrolled schema-4 SERVING successor has expired
- **THEN** automatic recovery requires current operation, claim, fence, bound PVC/image and maintenance authority, closed routes with external rejection proof, and no remaining PVC-using pods
- **AND** guarded revision-checked custody reissuance preserves the signing key, attachment, activation tuple, schema, software and membership lineage
- **AND** expired signing keys and future or malformed authorization windows remain invalid
- **AND** the reissued and subsequent serving windows end within the signing key's validity, with insufficient remaining lifetime kept fenced
- **AND** confirmation progress is retained until the exact fully DRAINING successor is published or reconciled; an uncertain acknowledgement cannot authorize target start
- **AND** the selected runtime must prove fresh private governance readiness against the authoritative Secret before admission or routes reopen
- **AND** recovery neither resets enrollment nor restores old data, repeats cutover or relaxes ordinary serving renewal

#### Scenario: A prepared but unenrolled migration outlasts custody

- **WHEN** retained preparation progress is still unenrolled and its custody expires
- **THEN** the cell remains fenced for explicit verified abort or recovery
- **AND** automatic retry cannot renew the prepared source, replace its plan or treat the elapsed window as enrollment authority

#### Scenario: Recovery custody publication succeeds without acknowledgement

- **WHEN** a target-start recovery Secret CAS has an uncertain acknowledgement, including across worker restart
- **THEN** its exact successor revision and chosen issuance time were durably committed before the first CAS in the same migration checkpoint
- **AND** retry accepts only that exact successor or a predecessor whose deterministic successor reproduces the commitment
- **AND** key validity and physical authority are checked at the actual current time, not the committed issuance time
- **AND** an elapsed DRAINING window does not replace the uncertain commitment or authorize serving; an expired signing key still fences recovery
- **AND** the checkpoint retains the migration denial prefix, prior completion/confirmation phase, fingerprint, operation/PVC/image binding, source and plan within the existing storage bound

#### Scenario: Destruction or another cell worker precedes migration entry

- **WHEN** the first migration checkpoint would overlap non-final tenant destruction or another claimed operation on the same cell
- **THEN** the checkpoint transition is refused atomically before any migration Job is submitted
- **AND** reused provider operation IDs do not erase the distinction between internal operations

#### Scenario: Fixed migration Job is observed during recovery

- **WHEN** a fixed-slot migration Job is present, including a failed or terminating Job
- **THEN** its own operation identity and exact provider recovery envelope are authenticated before classification
- **AND** only the current governance migration with durable progress may reach its coordinator for exact reconciliation
- **AND** an authenticated foreign Job blocks effects while malformed or unauthenticated evidence is refused
- **AND** expected Job contention preserves the checkpoint without consuming the worker failure budget

#### Scenario: Exact migration Job fails after database commit

- **WHEN** the exact request-bound migration Job has terminal Failed status with no active or terminating pods, potentially after a committed database transaction
- **THEN** recovery authenticates all namespace-observed pods using the bound PVC or fixed slot before UID/resource-version-bound cleanup
- **AND** foreign or malformed execution metadata remains untouched and nonterminal pods keep recovery pending
- **AND** cleanup permits only the same-phase request replay and never synthesizes success, restores a predecessor database or advances the durable phase

#### Scenario: Migration Job deletion acknowledgement is lost

- **WHEN** the exact migration Job is already terminating or absent after an uncertain deletion acknowledgement
- **THEN** recovery waits without deleting a replacement or issuing another delete for the terminating Job
- **AND** Job absence is insufficient until a namespace-wide observation proves no remaining bound-PVC or fixed-slot pods, including pods with missing labels
- **AND** the same absence proof precedes any replacement submission, while failed API observations never count as absence

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
