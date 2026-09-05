## ADDED Requirements

### Requirement: Executable marketplace reviewer fixture

The system SHALL maintain a versioned marketplace reviewer fixture whose complete pre-seeded payload is accepted by the current governed product write contract. Fixture validation MUST execute each exact checked note through `remember` validation and commit, including the canonical reviewed-none handshake only when validation requires it, and MUST read the committed note back without using a fixture exemption or privileged storage path.

#### Scenario: Checked fixture conforms to the real write contract

- **WHEN** release validation runs against the checked marketplace reviewer fixture in a newly initialized vault
- **THEN** every pre-seeded note is validated, committed, and read back through the real product leaves
- **AND** the resulting title and Markdown body match the exact checked payload

#### Scenario: Fixture contains a semantically invalid compiled note

- **WHEN** any pre-seeded note lacks a valid semantic unit or has another non-review write blocker
- **THEN** executable fixture validation fails before a candidate or directory packet is accepted
- **AND** validation does not add an exemption, reclassify the note as a raw source, or weaken the shared write contract

#### Scenario: Disconnected fixture note needs reviewed-none

- **WHEN** validation reports that an otherwise valid exact fixture note requires a relation-review disposition
- **THEN** the seeder commits the unchanged returned draft using `reviewed_none`, the returned relation-review hash, and a bounded generic reason
- **AND** it does not guess or fabricate a relation

#### Scenario: Fixture payload changes

- **WHEN** the content or seed order of the canonical reviewer payload changes
- **THEN** its fixture version and payload digest change together
- **AND** stale review cases, directory packets, credentials, and evidence cannot bind the new fixture

### Requirement: Reviewer bootstrap consumes an authority, not an OAuth token

The reviewer bootstrap SHALL NOT attempt an OAuth token exchange for the redeemed bootstrap authorization code, because the control plane refuses a bootstrap grant unconditionally. The bootstrap's outcome SHALL be the consumed authority read back from the operator surface, and the bootstrap MUST wait for the dedicated reviewer cell to reach `CELL_READY` before issuing any provider reviewer credential. Issuing a provider credential MUST remain the sealing event that revokes temporary setup access.

#### Scenario: Bootstrap redeems the invite

- **WHEN** invite redemption succeeds and returns a destination carrying an authorization code
- **THEN** the bootstrap makes no request to the OAuth token endpoint
- **AND** it does not require, retain, or report an access or refresh token

#### Scenario: Bootstrap records the promotion handoff

- **WHEN** the bootstrap authority reaches `consumed`
- **THEN** the bootstrap writes the outcome tenant, assignment and assignment generation to protected operator state
- **AND** that file is the sole handoff the promotion evidence tooling reads

#### Scenario: Readiness fails

- **WHEN** owner status does not reach `ready` with `CELL_READY` inside the staged-release reserve
- **THEN** the bootstrap fails closed without calling the reviewer-credential issuance endpoint
- **AND** it does not print bootstrap completion

#### Scenario: Fixture seeding is not performed by the bootstrap

- **WHEN** the checked reviewer fixture must be present in the reviewer vault
- **THEN** the bootstrap neither seeds nor verifies it, and does not report it as seeded
- **AND** the executable fixture definition remains the contract any future seeding must satisfy, with no production caller driving it against a live reviewer cell today

### Requirement: Claude-only cohort promotion

The promotion harness SHALL support promoting a Claude-only cohort. Supplying an OpenAI connector and an OpenAI artifact SHALL be opt-in, and a promotion request for a Claude-only cohort MUST omit the OpenAI artifact and evidence keys entirely rather than sending them null.

#### Scenario: Bootstrap without an OpenAI connector

- **WHEN** the bootstrap runs without an OpenAI connector argument
- **THEN** it resolves no ChatGPT connector document and creates exactly one sibling stage and one canary credential, both for Claude
- **AND** it records only the Claude sibling stage id for the later evidence run

#### Scenario: Promotion without an OpenAI artifact

- **WHEN** the promotion state directory holds a Claude artifact and evidence record but no OpenAI pair
- **THEN** the promote request omits `openaiArtifactId` and `openaiEvidence`
- **AND** the control plane promotes the candidate to live and admission opens

#### Scenario: Half-present OpenAI pair

- **WHEN** the promotion state directory holds exactly one of the OpenAI artifact and its evidence record
- **THEN** promotion refuses and names the missing file
- **AND** it does not silently promote a Claude-only cohort in place of the operator's intent

### Requirement: Reviewer bootstrap is resumable after a stranded tenant

The harness SHALL provide a `reset` command that releases a reviewer tenant stranded by a failed attempt, using the operator bearer token alone. It MUST drive the existing expired-reviewer-cleanup control, ending a still-live reviewer assignment through the exact existing `fail-assignment` transition first, and MUST take its target from the consumed bootstrap authority record rather than from any operator-supplied tenant, cell or operation identifier. It MUST print what it will release before releasing it.

#### Scenario: Stranded tenant holds a bound, ready cell

- **WHEN** a failed attempt leaves a reviewer tenant whose sole cell is bound, active, routable and `CELL_READY`
- **THEN** `reset` obtains eligibility from the read-only cleanup preflight and then enqueues the cleanup
- **AND** the tenant moves to `deletion_pending` with its fence advanced and its access lineage revoked, with no owner session and no emailed confirmation token involved

#### Scenario: Reviewer assignment has not yet expired

- **WHEN** the reviewer assignment is still `preparing` or `active`
- **THEN** `reset` ends it through `fail-assignment` at the operator-supplied version before the cleanup preflight
- **AND** that transition does not extend the assignment's immutable expiry

#### Scenario: Nothing to release

- **WHEN** no consumed bootstrap authority carries a recorded outcome operation
- **THEN** `reset` refuses before calling the preflight
- **AND** it cannot be pointed at a tenant that is not a reviewer bootstrap, because it accepts no tenant, cell or operation identifier

#### Scenario: Cleanup preflight refuses

- **WHEN** the read-only cleanup preflight reports the source operation ineligible
- **THEN** `reset` stops without calling the mutation
- **AND** it does not retry with altered selectors, because the refusal is deliberately non-diagnostic

#### Scenario: Identifiers absent from every admin route

- **WHEN** `reset` needs the tenant fence generation or the reviewer assignment version
- **THEN** the operator supplies them, because no admin route reports either
- **AND** a wrong value costs only a refused read-only preflight

#### Scenario: What reset does not reclaim

- **WHEN** an attempt has failed
- **THEN** the spent invite, email alias, staged release and OAuth client remain spent
- **AND** a tenant that is neither `active` with a bound cell nor at `candidate-cleanup` with no bound cell is outside both cleanup branches
